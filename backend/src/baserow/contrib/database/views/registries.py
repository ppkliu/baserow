from typing import Callable, Union, List

from django.contrib.auth.models import User as DjangoUser
from django.db import models as django_models
from rest_framework.fields import CharField

from rest_framework.serializers import Serializer

from baserow.contrib.database.fields.field_filters import OptionallyAnnotatedQ
from baserow.core.registry import (
    Instance,
    Registry,
    ModelInstanceMixin,
    ModelRegistryMixin,
    CustomFieldsInstanceMixin,
    CustomFieldsRegistryMixin,
    APIUrlsRegistryMixin,
    APIUrlsInstanceMixin,
    ImportExportMixin,
    MapAPIExceptionsInstanceMixin,
)
from baserow.contrib.database.fields import models as field_models

from .exceptions import (
    ViewTypeAlreadyRegistered,
    ViewTypeDoesNotExist,
    ViewFilterTypeAlreadyRegistered,
    ViewFilterTypeDoesNotExist,
    AggregationTypeDoesNotExist,
    AggregationTypeAlreadyRegistered,
)


class ViewType(
    MapAPIExceptionsInstanceMixin,
    APIUrlsInstanceMixin,
    CustomFieldsInstanceMixin,
    ModelInstanceMixin,
    ImportExportMixin,
    Instance,
):
    """
    This abstract class represents a custom view type that can be added to the
    view type registry. It must be extended so customisation can be done. Each view type
    will have his own model that must extend the View model, this is needed so that the
    user can set custom settings per view instance he has created.

    The added API urls will be available under the namespace 'api:database:views'.
    So if a url with name 'example' is returned by the method it will available under
    reverse('api:database:views:example').

    Example:
        from baserow.contrib.database.views.models import View
        from baserow.contrib.database.views.registry import ViewType, view_type_registry

        class ExampleViewModel(ViewType):
            pass

        class ExampleViewType(ViewType):
            type = 'unique-view-type'
            model_class = ExampleViewModel
            allowed_fields = ['example_ordering']
            serializer_field_names = ['example_ordering']
            serializer_field_overrides = {
                'example_ordering': serializers.CharField()
            }

            def get_api_urls(self):
                return [
                    path('view-type/', include(api_urls, namespace=self.type)),
                ]

        view_type_registry.register(ExampleViewType())
    """

    can_filter = True
    """
    Indicates if the view supports filters. If not, it will not be possible to add
    filter to the view.
    """

    can_sort = True
    """
    Indicates if the view support sortings. If not, it will not be possible to add a
    sort to the view.
    """

    can_aggregate_field = False
    """
    Indicates if the view supports field aggregation. If not, it will not be possible
    to compute fields aggregation for this view type.
    """

    can_share = False
    """
    Indicates if the view supports being shared via a public link.
    """

    field_options_model_class = None
    """
    The model class of the through table that contains the field options. The model
    must have a foreign key to the field and to the view.
    """

    field_options_serializer_class = None
    """
    The serializer class to serialize the field options model. It will be used in the
    API to update and list the field option, but it is also used to broadcast field
    option changes.
    """

    restrict_link_row_public_view_sharing = True
    """
    If a view is shared publicly, the `PublicViewLinkRowFieldLookupView` exposes all
    the primary values of every visible `link_row` field in the view. This property
    indicates whether the values should be restricted to existing relationships to
    the view.
    """

    when_shared_publicly_requires_realtime_events = True
    """
    If a view is shared publicly, this controls whether or not realtime row, field
    and view events will be available to subscribe to and sent to said subscribers.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.can_share:
            self.allowed_fields = self.allowed_fields + ["public"]
            self.serializer_field_names = self.serializer_field_names + [
                "public",
                "slug",
            ]
            self.serializer_field_overrides = {
                **self.serializer_field_overrides,
                "slug": CharField(
                    read_only=True,
                    help_text="The unique slug that can be used to construct a public "
                    "URL.",
                ),
            }

    def export_serialized(self, view, files_zip, storage):
        """
        Exports the view to a serialized dict that can be imported by the
        `import_serialized` method. This dict is also JSON serializable.

        :param view: The view instance that must be exported.
        :type view: View
        :param files_zip: A zip file buffer where the files related to the export
            must be copied into.
        :type files_zip: ZipFile
        :param storage: The storage where the files can be loaded from.
        :type storage: Storage or None
        :return: The exported view.
        :rtype: dict
        """

        serialized = {
            "id": view.id,
            "type": self.type,
            "name": view.name,
            "order": view.order,
        }

        if self.can_filter:
            serialized["filter_type"] = view.filter_type
            serialized["filters_disabled"] = view.filters_disabled
            serialized["filters"] = [
                {
                    "id": view_filter.id,
                    "field_id": view_filter.field_id,
                    "type": view_filter.type,
                    "value": view_filter_type_registry.get(
                        view_filter.type
                    ).get_export_serialized_value(view_filter.value),
                }
                for view_filter in view.viewfilter_set.all()
            ]

        if self.can_sort:
            serialized["sortings"] = [
                {"id": sort.id, "field_id": sort.field_id, "order": sort.order}
                for sort in view.viewsort_set.all()
            ]

        if self.can_share:
            serialized["public"] = view.public

        return serialized

    def import_serialized(
        self, table, serialized_values, id_mapping, files_zip, storage
    ):
        """
        Imported an exported serialized view dict that was exported via the
        `export_serialized` method. Note that all the fields must be imported first
        because we depend on the new field id to be in the mapping.

        :param table: The table where the view should be added to.
        :type table: Table
        :param serialized_values: The exported serialized view values that need to
            be imported.
        :type serialized_values: dict
        :param id_mapping: The map of exported ids to newly created ids that must be
            updated when a new instance has been created.
        :type id_mapping: dict
        :param files_zip: A zip file buffer where files related to the export can be
            extracted from.
        :type files_zip: ZipFile
        :param storage: The storage where the files can be copied to.
        :type storage: Storage or None
        :return: The newly created view instance.
        :rtype: View
        """

        from .models import ViewFilter, ViewSort

        if "database_views" not in id_mapping:
            id_mapping["database_views"] = {}
            id_mapping["database_view_filters"] = {}
            id_mapping["database_view_sortings"] = {}

        serialized_copy = serialized_values.copy()
        view_id = serialized_copy.pop("id")
        serialized_copy.pop("type")
        filters = serialized_copy.pop("filters") if self.can_filter else []
        sortings = serialized_copy.pop("sortings") if self.can_sort else []
        view = self.model_class.objects.create(table=table, **serialized_copy)
        id_mapping["database_views"][view_id] = view.id

        if self.can_filter:
            for view_filter in filters:
                view_filter_type = view_filter_type_registry.get(view_filter["type"])
                view_filter_copy = view_filter.copy()
                view_filter_id = view_filter_copy.pop("id")
                view_filter_copy["field_id"] = id_mapping["database_fields"][
                    view_filter_copy["field_id"]
                ]
                view_filter_copy[
                    "value"
                ] = view_filter_type.set_import_serialized_value(
                    view_filter_copy["value"], id_mapping
                )
                view_filter_object = ViewFilter.objects.create(
                    view=view, **view_filter_copy
                )
                id_mapping["database_view_filters"][
                    view_filter_id
                ] = view_filter_object.id

        if self.can_sort:
            for view_sort in sortings:
                view_sort_copy = view_sort.copy()
                view_sort_id = view_sort_copy.pop("id")
                view_sort_copy["field_id"] = id_mapping["database_fields"][
                    view_sort_copy["field_id"]
                ]
                view_sort_object = ViewSort.objects.create(view=view, **view_sort_copy)
                id_mapping["database_view_sortings"][view_sort_id] = view_sort_object.id

        return view

    def get_visible_fields_and_model(self, view):
        """
        Returns the field objects for the provided view. Depending on the view type this
        will only return the visible or appropriate fields as different view types can
        hide or show fields based on their configuration.

        :param view: The view to get the fields for.
        :type view: View
        :return: An ordered list of field objects for this view and the model for the
            view.
        :rtype list, Model
        """

        model = view.table.get_model()
        return model._field_objects.values(), model

    def get_field_options_serializer_class(self, create_if_missing):
        """
        Generates a serializer that has the `field_options` property as a
        `FieldOptionsField`. This serializer can be used by the API to validate or list
        the field options.

         :param create_if_missing: Whether or not to create any missing field options
            when looking them up during serialization.
        :type create_if_missing: bool
        :raises ValueError: When the related view type does not have a field options
            serializer class.
        :return: The generated serializer.
        :rtype: Serializer
        """

        from baserow.contrib.database.api.views.serializers import FieldOptionsField

        if not self.field_options_serializer_class:
            raise ValueError(
                f"The view type {self.type} does not have a field options serializer "
                f"class."
            )

        meta = type(
            "Meta",
            (),
            {"ref_name": self.type + "_view_field_options"},
        )

        attrs = {
            "Meta": meta,
            "field_options": FieldOptionsField(
                serializer_class=self.field_options_serializer_class,
                create_if_missing=create_if_missing,
            ),
        }

        return type(
            str("Generated" + self.type.capitalize() + "ViewFieldOptionsSerializer"),
            (Serializer,),
            attrs,
        )

    def before_field_options_update(self, view, field_options, fields):
        """
        Called before the field options are updated related to the provided view.

        :param view: The view for which the field options need to be updated.
        :type view: View
        :param field_options: A dict with the field ids as the key and a dict
            containing the values that need to be updated as value.
        :type field_options: dict
        :param fields: Optionally a list of fields can be provided so that they don't
            have to be fetched again.
        :return: The updated field options.
        :rtype: dict
        """

        return field_options

    def after_field_type_change(self, field: field_models.Field) -> None:
        """
        This hook is called after the type of a field has changed and gives the
        possibility to check compatibility with view stuff like specific field options.

        :param field: The concerned field.
        """

    def prepare_values(self, values: dict, table, user: DjangoUser):
        """
        The prepare_values hook gives the possibility to change the provided values
        just before they are going to be used to create or update the instance. For
        example if an ID is provided, it can be converted to a model instance. Or to
        convert a certain date string to a date object.

        :param values: The provided values.
        :param table: The table where the view is created in.
        :type table: Table
        :param user: The user on whose behalf the change is made.
        :return: The updates values.
        :type: dict
        """

        return values

    def view_created(self, view):
        """
        A hook that's called when a new view is created.

        :param view: The newly created view instance.
        """

    def get_visible_field_options_in_order(self, view):
        """
        Should return a queryset of all field options which are visible in the
        provided view and in the order they appear in the view.

        :param view: The view to query.
        :type view: View
        :return: A queryset of the views specific view options which are 'visible'
            and in order.
        """

        raise NotImplementedError(
            "An exportable or publicly sharable view must implement "
            "`get_visible_field_options_in_order`"
        )

    def get_hidden_field_options(self, view):
        """
        Should return a queryset of all field options which are hidden in the
        provided view.

        :param view: The view to query.
        :type view: View
        :return: A queryset of the views specific view options which are 'hidden'.
        """

        raise NotImplementedError(
            "An exportable or publicly sharable view must implement "
            "`get_hidden_field_options`"
        )


class ViewTypeRegistry(
    APIUrlsRegistryMixin, CustomFieldsRegistryMixin, ModelRegistryMixin, Registry
):
    """
    With the view type registry it is possible to register new view types.  A view type
    is an abstraction made specifically for Baserow. If added to the registry a user can
    create new views based on this type.
    """

    name = "view"
    does_not_exist_exception_class = ViewTypeDoesNotExist
    already_registered_exception_class = ViewTypeAlreadyRegistered

    def get_field_options_serializer_map(self):
        return {
            view_type.type: view_type.get_field_options_serializer_class(
                create_if_missing=False
            )
            for view_type in self.registry.values()
        }


class ViewFilterType(Instance):
    """
    This abstract class represents a view filter type that can be added to the view
    filter type registry. It must be extended so customisation can be done. Each view
    filter type will have its own type names and rules. The get_filter method should
    be overwritten and should return a Q object that can be applied to the queryset
    later.

    Example:
        from baserow.contrib.database.views.registry import (
            ViewFilterType, view_filter_type_registry
        )

        class ExampleViewFilterType(ViewFilterType):
            type = 'equal'
            compatible_field_types = ['text', 'long_text']

            def get_filter(self, field_name, value):
                return Q(**{
                    field_name: value
                })

        view_filter_type_registry.register(ExampleViewFilterType())
    """

    compatible_field_types: List[
        Union[str, Callable[["field_models.Field"], bool]]
    ] = []
    """
    Defines which field types are compatible with the filter. Only the supported ones
    can be used in combination with the field. The values in this list can either be
    the literal field_type.type string, or a callable which takes the field being
    checked and returns True if compatible or False if not.
    """

    def get_filter(self, field_name, value, model_field, field) -> OptionallyAnnotatedQ:
        """
        Should return either a Q object or and AnnotatedQ containing the requested
        filtering and annotations based on the provided arguments.

        :param field_name: The name of the field that needs to be filtered.
        :type field_name: str
        :param value: The value that the field must be compared to.
        :type value: str
        :param model_field: The field extracted from the model.
        :type model_field: models.Field
        :param field: The instance of the underlying baserow field.
        :type field: Field
        :return: A Q or AnnotatedQ filter for this specific field, which will be then
            later combined with other filters to generate the final total view filter.
        """

        raise NotImplementedError("Each must have his own get_filter method.")

    def get_preload_values(self, view_filter) -> dict:
        """
        Optionally a view filter type can preload certain values for displaying
        purposes. This is for example used by the `link_row_has` filter type to
        preload the name of the chosen row.

        :param view_filter: The view filter instance object where the values must be
            preloaded for.
        :type view_filter: ViewFilter
        :return: The preloaded values as a dict.
        """

        return {}

    def get_export_serialized_value(self, value) -> str:
        """
        This method is called before the filter value is exported. Here it can
        optionally be modified.

        :param value: The original value.
        :type value: str
        :return: The updated value.
        :rtype: str
        """

        return value

    def set_import_serialized_value(self, value, id_mapping) -> str:
        """
        This method is called before a field is imported. It can optionally be
        modified. If the value for example points to a field or select option id, it
        can be replaced with the correct value by doing a lookup in the id_mapping.

        :param value: The original exported value.
        :type value: str
        :param id_mapping: The map of exported ids to newly created ids that must be
            updated when a new instance has been created.
        :type id_mapping: dict
        :return: The new value that will be imported.
        :rtype: str
        """

        return value

    def field_is_compatible(self, field):
        """
        Given a particular instance of a field returns a list of Type[FieldType] which
        are compatible with this particular field type.

        Works by checking the field_type against this view filters list of compatible
        field types or compatibility checking functions defined in
        self.allowed_field_types.

        :param field: The field to check.
        :return: True if the field is compatible, False otherwise.
        """

        from baserow.contrib.database.fields.registries import field_type_registry

        field_type = field_type_registry.get_by_model(field.specific_class)

        return any(
            callable(t) and t(field) or t == field_type.type
            for t in self.compatible_field_types
        )


class ViewFilterTypeRegistry(Registry):
    """
    With the view filter type registry is is possible to register new view filter
    types. A view filter type is an abstractions that allows different types of
    filtering for rows in a view. It is possible to add multiple view filters to a view
    and all the rows must match those filters.
    """

    name = "view_filter"
    does_not_exist_exception_class = ViewFilterTypeDoesNotExist
    already_registered_exception_class = ViewFilterTypeAlreadyRegistered


class ViewAggregationType(Instance):
    """
    If you want to aggregate the values of fields in a view, you can use a field
    aggregation. For example you can compute a sum of all values of a field in a table.
    """

    def get_aggregation(
        self,
        field_name: str,
        model_field: django_models.Field,
        field: field_models.Field,
    ) -> django_models.Aggregate:
        """
        Should return the requested django aggregation object based on
        the provided arguments.

        :param field_name: The name of the field that needs to be aggregated.
        :type field_name: str
        :param model_field: The field extracted from the model.
        :type model_field: django_models.Field
        :param field: The instance of the underlying baserow field.
        :type field: Field
        :return: A django aggregation object for this specific field.
        """

        raise NotImplementedError(
            "Each aggregation type must have his own get_aggregation method."
        )

    def field_is_compatible(self, field: field_models.Field) -> bool:
        """
        Given a particular instance of a field returns whether the field is supported
        by this aggregation type or not.

        :param field: The field to check.
        :return: True if the field is compatible, False otherwise.
        """

        from baserow.contrib.database.fields.registries import field_type_registry

        field_type = field_type_registry.get_by_model(field.specific_class)

        return any(
            callable(t) and t(field) or t == field_type.type
            for t in self.compatible_field_types
        )


class ViewAggregationTypeRegistry(Registry):
    """
    This registry contains all the available field aggregation operators. A field
    aggregation allow to summarize all the values for a specific field of a table.
    """

    name = "field_aggregation"
    does_not_exist_exception_class = AggregationTypeDoesNotExist
    already_registered_exception_class = AggregationTypeAlreadyRegistered


# A default view type registry is created here, this is the one that is used
# throughout the whole Baserow application to add a new view type.
view_type_registry = ViewTypeRegistry()
view_filter_type_registry = ViewFilterTypeRegistry()
view_aggregation_type_registry = ViewAggregationTypeRegistry()
